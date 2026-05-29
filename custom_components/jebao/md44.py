"""Jebao MD-4.4 4-channel dosing pump.

The MD-4.4 doser uses a different wire format from the MDP-20000 wavemaker
even though both speak the same outer Gizwits LAN protocol on TCP/12416.

Frame layout (this module's view):
    00 00 00 03 | LL (varint) | 00 00 | CMD | body...

Differences from the wavemaker handled by python-jebao:
  * Length is varint (typically 2 bytes), not a fixed 1 byte.
  * Status response is ~796 bytes carrying packed bit flags, four 1-byte day
    intervals, calibration value, four 96-byte schedule blobs, plus YMD/HMS.
  * Boolean writes use a 24-bit mask + 24-bit state pattern (see
    ``_build_bit_write_frame``); each of switch/channe1..4/Timer1..4ON has a
    dedicated bit index, and the value bit is the mask bit shifted left by 8.

Datapoint map was extracted from the Jebao Aqua APK
(productConfig/25c5b146...json, "滴定泵_有AP校时" — dosing pump with AP time
sync). The boolean write format and the schedule layout were cross-checked
against tancou/jebao-dosing-pump-md-4.4.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import List, Optional

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 12416
DEFAULT_TIMEOUT = 5.0

# Bit indices in the 16-bit state word (LSB-first within each byte).
# Bits 0..7 live in pumpBlock0 (low byte, body[5] of the 0x91 response),
# bits 8..11 live in pumpBlock1 (high byte, body[4]).
BIT_SWITCH = 0
BIT_CHANNEL = [1, 2, 3, 4]          # channe1..channe4
BIT_TIMER = [5, 6, 7, 8]            # Timer1ON..Timer4ON
BIT_CALSW = 9
BIT_CALSET_LO = 10                  # CALSet is a 2-bit enum at bits 10..11

# Each channel's CH*SWTime blob is 96 bytes. The first byte is unused / a
# spare; the next 24 entries are 4 bytes each: (hour, minute, quantity_hi,
# quantity_lo). Quantity is in 0.1 mL units in the firmware we tested.
SCHEDULE_BLOB_LEN = 96
SCHEDULES_PER_CHANNEL = 24
SCHEDULE_ENTRY_LEN = 4

# Total body length the pump's write frames use. 0x19a = 410, matching the
# fixed P0 buffer size the firmware expects on 0x93 / sub-cmd 0x01 writes.
WRITE_BODY_LEN = 410


class MD44Error(Exception):
    """Base error for MD-4.4 protocol issues."""


class MD44ConnectionError(MD44Error):
    """Network-level failure talking to the pump."""


class MD44AuthError(MD44Error):
    """Passcode handshake failed."""


@dataclass
class ScheduleEntry:
    """One dosing schedule entry on a channel."""

    hour: int
    minute: int
    quantity: int  # in 0.1 mL units (firmware native)

    @property
    def quantity_ml(self) -> float:
        return self.quantity / 10.0


@dataclass
class MD44State:
    """Parsed pump state."""

    master_on: bool = False
    channels: List[bool] = field(default_factory=lambda: [False] * 4)
    timers_enabled: List[bool] = field(default_factory=lambda: [False] * 4)
    cal_switch: bool = False
    cal_set: int = 0  # 0..3 → calibration channel 1..4
    intervals_days: List[int] = field(default_factory=lambda: [0] * 4)
    calib1: int = 0
    schedules: List[List[ScheduleEntry]] = field(
        default_factory=lambda: [[], [], [], []]
    )
    ymd: bytes = b"\x00\x00\x00\x00"
    hms: bytes = b"\x00\x00\x00\x00"
    open_circuit: bool = False
    fault_uart: bool = False

    def as_dict(self) -> dict:
        return {
            "master_on": self.master_on,
            "channels": list(self.channels),
            "timers_enabled": list(self.timers_enabled),
            "cal_switch": self.cal_switch,
            "cal_set": self.cal_set,
            "intervals_days": list(self.intervals_days),
            "calib1": self.calib1,
            "schedules": [
                [(s.hour, s.minute, s.quantity) for s in chan]
                for chan in self.schedules
            ],
            "ymd": self.ymd.hex(),
            "hms": self.hms.hex(),
            "open_circuit": self.open_circuit,
            "fault_uart": self.fault_uart,
        }


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


class MD44Device:
    """High-level client for the Jebao MD-4.4 dosing pump."""

    model = "MD-4.4"

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        device_id: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.device_id = device_id
        self.state = MD44State()

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._passcode: Optional[bytes] = None

    # ------------------------------------------------------------------ conn

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=timeout
            )
        except asyncio.TimeoutError as err:
            raise MD44ConnectionError(
                f"Timeout connecting to {self.host}:{self.port}"
            ) from err
        except OSError as err:
            raise MD44ConnectionError(
                f"Failed to connect to {self.host}: {err}"
            ) from err

        try:
            await self._authenticate(timeout)
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # pylint: disable=broad-except
                pass
        self._writer = None
        self._reader = None

    async def __aenter__(self) -> "MD44Device":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------ wire

    async def _readexactly(self, n: int) -> bytes:
        assert self._reader is not None
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as err:
            raise MD44ConnectionError("Connection closed by pump") from err

    async def _read_varint(self) -> int:
        value = 0
        shift = 0
        for _ in range(5):  # 5 bytes covers anything up to 35 bits
            b = await self._readexactly(1)
            value |= (b[0] & 0x7F) << shift
            if not (b[0] & 0x80):
                return value
            shift += 7
        raise MD44ConnectionError("Varint too long")

    async def _read_frame(self) -> bytes:
        """Read one full frame and return its body (everything after the
        magic+length prefix). The 2-byte ``00 00`` reserved field and the
        1-byte cmd are still part of the returned bytes — callers index
        into them directly."""
        magic = await self._readexactly(4)
        if magic != b"\x00\x00\x00\x03":
            raise MD44ConnectionError(f"Bad magic: {magic.hex()}")
        length = await self._read_varint()
        return await self._readexactly(length)

    async def _send(self, data: bytes) -> None:
        assert self._writer is not None
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as err:  # pylint: disable=broad-except
            raise MD44ConnectionError(f"Send failed: {err}") from err

    async def _drain_unsolicited(self, window: float = 0.3) -> None:
        """Swallow any unsolicited frames the pump pushes after auth or after
        a control command. The MD-4.4 sends a 0x09 echo and a 0x62 device
        announcement immediately after login, plus a 0x91 push whenever the
        state changes."""
        assert self._reader is not None
        try:
            while True:
                magic = await asyncio.wait_for(
                    self._reader.readexactly(4), timeout=window
                )
                if magic != b"\x00\x00\x00\x03":
                    raise MD44ConnectionError(
                        f"Stream lost sync: {magic.hex()}"
                    )
                length = await self._read_varint()
                await self._readexactly(length)
        except (asyncio.TimeoutError, MD44ConnectionError):
            return

    # ------------------------------------------------------------------ auth

    async def _authenticate(self, timeout: float) -> None:
        await self._send(bytes.fromhex("0000000303000006"))
        body = await asyncio.wait_for(self._read_frame(), timeout=timeout)
        # body = 00 00 07 00 0a <10-byte passcode>
        if len(body) < 15 or body[2] != 0x07:
            raise MD44AuthError(
                f"Bad passcode response (type 0x{body[2]:02x})"
            )
        self._passcode = bytes(body[5:15])
        await self._send(
            bytes.fromhex("000000030f000008000a") + self._passcode
        )
        body = await asyncio.wait_for(self._read_frame(), timeout=timeout)
        if len(body) < 4 or body[2] != 0x09 or body[3] != 0x00:
            raise MD44AuthError("Login rejected")
        await self._drain_unsolicited()
        _LOGGER.info("MD-4.4 %s authenticated at %s", self.device_id or "?", self.host)

    # ----------------------------------------------------------------- read

    async def update(self, timeout: float = DEFAULT_TIMEOUT) -> MD44State:
        """Refresh ``self.state`` from the pump and return it."""
        async with self._lock:
            if not self.is_connected:
                raise MD44ConnectionError("Not connected")
            await self._send(bytes.fromhex("000000030400009002"))
            body = await asyncio.wait_for(self._read_frame(), timeout=timeout)
            self.state = self._parse_status(body)
            # Pump often pushes a duplicate frame right after.
            await self._drain_unsolicited(window=0.2)
            return self.state

    @staticmethod
    def _parse_status(body: bytes) -> MD44State:
        """Parse a 0x91 status response body.

        Layout in absolute frame coordinates (body offsets in parens):
          [4]  (0)   00
          [5]  (1)   00
          [6]  (2)   91  cmd
          [7]  (3)   03  sub-cmd / version flag
          [8..11] (4..7)   SN (we ignore it)
          [10]  (4)  pumpBlock1 (state byte 1, bits 8..11)
          [11]  (5)  pumpBlock0 (state byte 0, bits 0..7)
          [12..15] (6..9)  IntervalT1..IntervalT4
          [16]  (10)  Calib1
          [17..112] (11..106)   CH1SWTime (96 bytes)
          [113..208] (107..202) CH2SWTime
          [209..304] (203..298) CH3SWTime
          [305..400] (299..394) CH4SWTime
          [401..404] (395..398) YMDData
          [405..408] (399..402) HMSData
          [409]      (403)      time1 (read-only counter)
          [410]      (404)      OpenCircuit (bit 0)
          [411]      (405)      Fault_UART (bit 0)
        """
        if len(body) < 100 or body[2] != 0x91:
            # The MDP-20000 wavemaker also answers 0x90 requests with a 0x91
            # frame, but its body is ~12 bytes. Anything that short is the
            # wrong pump — reject it so config_flow can fall back.
            raise MD44Error(
                f"Unexpected response (type 0x{body[2] if len(body) > 2 else 0:02x}, "
                f"len {len(body)}) — probably not an MD-4.4"
            )

        pump_block_1 = body[4]
        pump_block_0 = body[5]

        state = MD44State()
        state.master_on = bool(pump_block_0 & (1 << BIT_SWITCH))
        state.channels = [
            bool(pump_block_0 & (1 << BIT_CHANNEL[0])),
            bool(pump_block_0 & (1 << BIT_CHANNEL[1])),
            bool(pump_block_0 & (1 << BIT_CHANNEL[2])),
            bool(pump_block_0 & (1 << BIT_CHANNEL[3])),
        ]
        state.timers_enabled = [
            bool(pump_block_0 & (1 << BIT_TIMER[0])),
            bool(pump_block_0 & (1 << BIT_TIMER[1])),
            bool(pump_block_0 & (1 << BIT_TIMER[2])),
            bool(pump_block_1 & (1 << (BIT_TIMER[3] - 8))),
        ]
        state.cal_switch = bool(pump_block_1 & (1 << (BIT_CALSW - 8)))
        state.cal_set = (pump_block_1 >> (BIT_CALSET_LO - 8)) & 0x03

        if len(body) >= 10:
            state.intervals_days = list(body[6:10])
        if len(body) >= 11:
            state.calib1 = body[10]

        sched_starts = [11, 107, 203, 299]
        state.schedules = []
        for start in sched_starts:
            blob = body[start : start + SCHEDULE_BLOB_LEN]
            state.schedules.append(MD44Device._parse_schedule(blob))

        if len(body) >= 403:
            state.ymd = bytes(body[395:399])
            state.hms = bytes(body[399:403])
        if len(body) >= 405:
            state.open_circuit = bool(body[404] & 0x01)
        if len(body) >= 406:
            state.fault_uart = bool(body[405] & 0x01)

        return state

    @staticmethod
    def _parse_schedule(blob: bytes) -> List[ScheduleEntry]:
        out: List[ScheduleEntry] = []
        # blob[0] is reserved/spare. Entries follow at blob[1..].
        for i in range(SCHEDULES_PER_CHANNEL):
            base = 1 + i * SCHEDULE_ENTRY_LEN
            if base + SCHEDULE_ENTRY_LEN > len(blob):
                break
            hour = blob[base]
            minute = blob[base + 1]
            quantity = (blob[base + 2] << 8) | blob[base + 3]
            if hour == 0 and minute == 0 and quantity == 0:
                continue
            out.append(ScheduleEntry(hour=hour, minute=minute, quantity=quantity))
        return out

    # ---------------------------------------------------------------- write

    async def _send_control(self, body: bytes) -> None:
        """Send a 0x93 control frame and wait for the 0x94 ack."""
        async with self._lock:
            if not self.is_connected:
                raise MD44ConnectionError("Not connected")
            frame = b"\x00\x00\x00\x03" + _encode_varint(len(body)) + body
            await self._send(frame)
            try:
                ack = await asyncio.wait_for(self._read_frame(), timeout=DEFAULT_TIMEOUT)
            except asyncio.TimeoutError as err:
                raise MD44Error("No ACK after control command") from err
            if len(ack) < 3 or ack[2] != 0x94:
                _LOGGER.warning(
                    "Control command got unexpected response type 0x%02x",
                    ack[2] if len(ack) > 2 else 0,
                )
            # Pump usually pushes an updated 0x91 right after the ack.
            await self._drain_unsolicited(window=0.4)

    @staticmethod
    def _build_bit_write_body(bit_index: int, value: bool) -> bytes:
        """Build the 410-byte body for a single-bit state write.

        ``bit_index`` corresponds to the schema's overall bit position
        (0 = switch, 1..4 = channe1..4, 5..7 = Timer1..3ON, 8 = Timer4ON).
        """
        if not 0 <= bit_index <= 23:
            raise ValueError(f"bit_index out of range: {bit_index}")
        mask = 1 << bit_index
        state = (1 << (bit_index + 8)) if value else 0
        body = bytearray()
        body += b"\x00\x00\x93"          # reserved + cmd
        body += b"\x00\x00\x00\x00"      # SN
        body += b"\x01"                  # sub-cmd: masked-bit write
        body += mask.to_bytes(3, "big")
        body += state.to_bytes(3, "big")
        if len(body) < WRITE_BODY_LEN:
            body += b"\x00" * (WRITE_BODY_LEN - len(body))
        return bytes(body)

    async def set_master(self, on: bool) -> None:
        _LOGGER.info("MD-4.4: master switch -> %s", on)
        await self._send_control(self._build_bit_write_body(BIT_SWITCH, on))

    async def set_channel(self, idx: int, on: bool) -> None:
        """Toggle one channel (idx is 0..3 for channels 1..4)."""
        if not 0 <= idx <= 3:
            raise ValueError(f"channel idx out of range: {idx}")
        _LOGGER.info("MD-4.4: channel %d -> %s", idx + 1, on)
        await self._send_control(
            self._build_bit_write_body(BIT_CHANNEL[idx], on)
        )

    async def set_timer_enabled(self, idx: int, on: bool) -> None:
        if not 0 <= idx <= 3:
            raise ValueError(f"timer idx out of range: {idx}")
        _LOGGER.info("MD-4.4: timer %d enabled -> %s", idx + 1, on)
        await self._send_control(
            self._build_bit_write_body(BIT_TIMER[idx], on)
        )

    async def set_cal_switch(self, on: bool) -> None:
        await self._send_control(self._build_bit_write_body(BIT_CALSW, on))

    async def ping(self) -> None:
        """Send a keepalive ping. Used to keep the TCP connection warm."""
        async with self._lock:
            if not self.is_connected:
                raise MD44ConnectionError("Not connected")
            await self._send(bytes.fromhex("0000000303000015"))
            try:
                ack = await asyncio.wait_for(self._read_frame(), timeout=2.0)
                if len(ack) < 3 or ack[2] != 0x16:
                    _LOGGER.warning(
                        "Ping got unexpected response 0x%02x",
                        ack[2] if len(ack) > 2 else 0,
                    )
            except asyncio.TimeoutError:
                raise MD44ConnectionError("Ping timeout")

    # ------------------------------------------------------------ helpers

    @staticmethod
    def encode_time(now: Optional[dt.datetime] = None) -> tuple[bytes, bytes]:
        """Build the YMDData / HMSData blobs the pump's time-sync uses.

        Returns ``(ymd, hms)`` where each blob is 4 bytes:
          ymd = year-2000 | month | day | weekday(0=Sun)
          hms = hour | minute | second | 0
        The write protocol for these isn't fully reverse-engineered yet,
        so this helper is exposed for callers but the actual write isn't
        wired into the device yet.
        """
        if now is None:
            now = dt.datetime.now()
        ymd = bytes([
            (now.year - 2000) & 0xFF,
            now.month,
            now.day,
            now.isoweekday() % 7,  # Mon=1..Sun=0
        ])
        hms = bytes([now.hour, now.minute, now.second, 0])
        return ymd, hms
